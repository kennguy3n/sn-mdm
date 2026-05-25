//! Pack ingest — reads JSONL output from the Python crawl pipeline
//! and writes it into the SQLCipher pack store.
//!
//! The ingest path is the consumer-side mirror of the Python
//! `crawl/pipeline.py` orchestrator. It expects three streams:
//!
//! * `packs/metadata/{publisher}.jsonl` — one [`Episode`] per line.
//! * `packs/chunks/{publisher}.jsonl`   — one [`Chunk`] per line.
//! * `packs/governance/rights_log.jsonl` — one [`GovernanceEntry`] per line.
//!
//! Three invariants are enforced on every ingest:
//!
//! 1. **Rights gate before chunking** — episodes whose
//!    `rights_code` is not in the configured allowlist are rejected
//!    with [`PackError::RightsGateRefused`] and their chunks are
//!    never inserted. The rejection is also recorded in the
//!    governance log.
//! 2. **BLAKE3 content-hash dedup** — chunks whose canonicalised
//!    text hashes to an already-present `content_hash` are skipped
//!    (the existing row already contains the same text), keeping
//!    the pack idempotent under re-ingestion. The `episode` row is
//!    still written because the episode's metadata may have
//!    changed (e.g. a new asset URL was discovered) even when its
//!    transcript text has not.
//! 3. **Append-only**. The schema triggers in
//!    [`crate::schema::SCHEMA_SQL`] reject UPDATE / DELETE on the
//!    `episode`, `chunk`, and `governance_log` tables. The
//!    in-process code reinforces that contract by going through
//!    `INSERT OR IGNORE` so a re-ingest of the same row is a
//!    silent no-op.

use std::io::BufRead;
use std::path::Path;

use chrono::Utc;
use rusqlite::{params, Connection, OpenFlags};

use crate::error::{PackError, Result};
use crate::metadata::{Chunk, Episode, GovernanceEntry};
use crate::schema::{CONNECTION_PRAGMAS, SCHEMA_SQL};
use crate::SCHEMA_VERSION;

/// Default rights-code allowlist applied by [`PackStore::ingest_episode`].
///
/// Codes intentionally omitted from the default allowlist:
///
/// * `paywalled` — we never crawl behind a paywall.
/// * `unknown`   — explicit rejection; the crawler must classify.
/// * `cc_by_nd`  — no-derivatives includes chunking; rejected by default.
pub const DEFAULT_RIGHTS_ALLOWLIST: &[&str] = &[
    "ogl_v3",
    "cc_by",
    "cc_by_sa",
    "cc_by_nc",
    "cc_by_nc_nd",
    "free_access_copyrighted",
    "public_domain",
];

/// In-process handle to one pack database. Wraps a SQLCipher
/// connection plus the configured rights allowlist.
///
/// `PackStore` is **not** `Send` / `Sync` — rusqlite's `Connection`
/// holds a `*mut sqlite3` so the type is `!Send`. Use one
/// `PackStore` per thread, or wrap in a `Mutex<PackStore>` for
/// shared access.
pub struct PackStore {
    conn: Connection,
    rights_allowlist: Vec<String>,
}

/// Per-publisher ingest counts. Returned by
/// [`PackStore::ingest_jsonl_files`].
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct IngestStats {
    /// Total episodes seen in the input file.
    pub episodes_seen: u64,
    /// Episodes that passed the rights gate and were inserted.
    pub episodes_inserted: u64,
    /// Episodes that were already in the store (dedup by
    /// `episode_id`).
    pub episodes_skipped_existing: u64,
    /// Episodes rejected by the rights gate. Each is recorded in
    /// the governance log with `deprecated = 1`.
    pub episodes_rejected_rights: u64,
    /// Total chunks seen.
    pub chunks_seen: u64,
    /// Chunks inserted.
    pub chunks_inserted: u64,
    /// Chunks skipped because their `content_hash` was already
    /// present (idempotent re-ingest).
    pub chunks_skipped_dedup: u64,
    /// Chunks skipped because their owning episode was rejected by
    /// the rights gate.
    pub chunks_skipped_rights: u64,
}

/// Aggregate ingest report across multiple publishers.
#[derive(Debug, Default, Clone)]
pub struct IngestReport {
    /// Per-publisher stats, keyed by the publisher segment of
    /// `episode_id`.
    pub by_publisher: std::collections::BTreeMap<String, IngestStats>,
}

impl IngestReport {
    /// Sum of all per-publisher stats.
    pub fn totals(&self) -> IngestStats {
        let mut out = IngestStats::default();
        for s in self.by_publisher.values() {
            out.episodes_seen += s.episodes_seen;
            out.episodes_inserted += s.episodes_inserted;
            out.episodes_skipped_existing += s.episodes_skipped_existing;
            out.episodes_rejected_rights += s.episodes_rejected_rights;
            out.chunks_seen += s.chunks_seen;
            out.chunks_inserted += s.chunks_inserted;
            out.chunks_skipped_dedup += s.chunks_skipped_dedup;
            out.chunks_skipped_rights += s.chunks_skipped_rights;
        }
        out
    }
}

impl PackStore {
    /// Open (or create) a pack store at `path`. Applies the schema
    /// in [`crate::schema::SCHEMA_SQL`] inside a single
    /// transaction, sets the connection pragmas, and stamps
    /// `PRAGMA user_version = SCHEMA_VERSION`.
    ///
    /// `passphrase` is fed to SQLCipher via `PRAGMA key`. Pass an
    /// empty string to open an unencrypted database (useful for
    /// unit tests; production deployments always pass a real key).
    pub fn open<P: AsRef<Path>>(path: P, passphrase: &str) -> Result<Self> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
        )?;
        Self::init_connection(&conn, passphrase)?;
        Self::apply_schema(&conn)?;
        Ok(Self {
            conn,
            rights_allowlist: DEFAULT_RIGHTS_ALLOWLIST
                .iter()
                .map(|s| (*s).to_string())
                .collect(),
        })
    }

    /// Open a pack store in memory. Used by unit tests and the
    /// pack-builder hot path (the FTS5 index is built in memory
    /// then serialised into the `.pack` file).
    pub fn open_in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory()?;
        // No passphrase on in-memory databases — SQLCipher accepts
        // an empty key but the connection has no on-disk surface
        // anyway.
        Self::init_connection(&conn, "")?;
        Self::apply_schema(&conn)?;
        Ok(Self {
            conn,
            rights_allowlist: DEFAULT_RIGHTS_ALLOWLIST
                .iter()
                .map(|s| (*s).to_string())
                .collect(),
        })
    }

    /// Replace the default rights allowlist. Pass a slice of
    /// rights codes that are acceptable for the current pack
    /// build. Codes not in the list are rejected by
    /// [`PackStore::ingest_episode`].
    pub fn set_rights_allowlist<I, S>(&mut self, codes: I)
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.rights_allowlist = codes.into_iter().map(Into::into).collect();
    }

    /// Borrow the underlying connection. Exposed so callers can
    /// implement custom read paths (e.g. analytics queries) without
    /// re-opening the database.
    pub fn connection(&self) -> &Connection {
        &self.conn
    }

    fn init_connection(conn: &Connection, passphrase: &str) -> Result<()> {
        if !passphrase.is_empty() {
            // Quote-escape the key per SQLCipher docs.
            let escaped = passphrase.replace('\'', "''");
            conn.execute_batch(&format!("PRAGMA key = '{escaped}';"))?;
        }
        conn.execute_batch(CONNECTION_PRAGMAS)?;
        Ok(())
    }

    fn apply_schema(conn: &Connection) -> Result<()> {
        // Bracket the schema apply in a transaction so a half-applied
        // schema can't leave the database wedged.
        conn.execute_batch("BEGIN;")?;
        let result: Result<()> = (|| {
            conn.execute_batch(SCHEMA_SQL)?;
            // Check / stamp the schema version.
            let mut found: i32 = conn.pragma_query_value(None, "user_version", |r| r.get(0))?;
            if found == 0 {
                conn.pragma_update(None, "user_version", SCHEMA_VERSION)?;
                found = SCHEMA_VERSION;
            }
            if found > SCHEMA_VERSION {
                return Err(PackError::IncompatibleSchema {
                    found,
                    expected: SCHEMA_VERSION,
                });
            }
            Ok(())
        })();
        match result {
            Ok(()) => {
                conn.execute_batch("COMMIT;")?;
                Ok(())
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK;");
                Err(e)
            }
        }
    }

    /// Run the rights gate against the supplied [`Episode`]. Returns
    /// `Ok(())` if the episode is admissible, or
    /// [`PackError::RightsGateRefused`] otherwise. Does not touch
    /// the database — callers compose this with logging /
    /// statistics tracking.
    pub fn check_rights_gate(&self, episode: &Episode) -> Result<()> {
        if self
            .rights_allowlist
            .iter()
            .any(|c| c.eq_ignore_ascii_case(&episode.rights_code))
        {
            Ok(())
        } else {
            Err(PackError::RightsGateRefused {
                episode_id: episode.episode_id.clone(),
                rights_code: episode.rights_code.clone(),
            })
        }
    }

    /// Ingest one episode. Runs the rights gate, inserts the
    /// `episode` row (idempotent on `episode_id`), and writes the
    /// matching governance-log entry. Returns `Ok(true)` if the row
    /// was inserted, `Ok(false)` if it was already present.
    ///
    /// Rights-rejected episodes return
    /// [`PackError::RightsGateRefused`] and write a governance-log
    /// row with `deprecated = 1` so the rejection is permanently
    /// audited.
    pub fn ingest_episode(&self, episode: &Episode) -> Result<bool> {
        match self.check_rights_gate(episode) {
            Ok(()) => {
                let metadata_json = serde_json::to_string(episode)?;
                let industry = serde_json::to_string(&episode.industry_tags)?;
                let function = serde_json::to_string(&episode.function_tags)?;
                let business = serde_json::to_string(&episode.business_model_tags)?;
                let geography = serde_json::to_string(&episode.country_region)?;
                let evidence = serde_json::to_string(std::slice::from_ref(&episode.source_type))?;
                let now = Utc::now().timestamp();
                let inserted = self
                    .conn
                    .prepare_cached(
                        r#"INSERT OR IGNORE INTO episode
                          (episode_id, publisher, series, title, metadata_json, rights_code,
                           industry_tags, function_tags, business_model_tags, geography_tags,
                           evidence_type, created_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"#,
                    )?
                    .execute(params![
                        episode.episode_id,
                        episode.publisher,
                        episode.series,
                        episode.title,
                        metadata_json,
                        episode.rights_code,
                        industry,
                        function,
                        business,
                        geography,
                        evidence,
                        now,
                    ])?;
                // Compute a placeholder content-hash for the episode-level
                // governance entry. The canonical chunk-level hashes are
                // written by `ingest_chunk`; this episode-level entry
                // gives the audit trail one row per ingest cadence.
                let content_hash = blake3::hash(episode.episode_id.as_bytes());
                self.append_governance(&GovernanceEntry {
                    episode_id: episode.episode_id.clone(),
                    rights_code: episode.rights_code.clone(),
                    ingestion_date: now,
                    content_hash: content_hash.to_hex().to_string(),
                    deprecated: false,
                })?;
                Ok(inserted == 1)
            }
            Err(PackError::RightsGateRefused {
                episode_id,
                rights_code,
            }) => {
                // Record the rejection in the audit log even though
                // no chunks will be inserted. `deprecated = 1`
                // marks the entry as "do not surface" without
                // changing the append-only contract.
                let now = Utc::now().timestamp();
                let content_hash = blake3::hash(episode_id.as_bytes());
                let entry = GovernanceEntry {
                    episode_id: episode_id.clone(),
                    rights_code: rights_code.clone(),
                    ingestion_date: now,
                    content_hash: content_hash.to_hex().to_string(),
                    deprecated: true,
                };
                self.append_governance(&entry)?;
                Err(PackError::RightsGateRefused {
                    episode_id,
                    rights_code,
                })
            }
            Err(other) => Err(other),
        }
    }

    /// Ingest one chunk. Computes the canonical BLAKE3 content
    /// hash, skips on dedup, and indexes into FTS5. Returns
    /// `Ok(true)` on insert, `Ok(false)` on dedup.
    ///
    /// The caller is responsible for ensuring the owning episode
    /// has been ingested (the foreign-key constraint will reject
    /// orphan chunks otherwise).
    pub fn ingest_chunk(&self, chunk: &Chunk) -> Result<bool> {
        let canonical = canonicalise_text(&chunk.chunk_text);
        let hash = blake3::hash(canonical.as_bytes());

        // Dedup: if this exact content_hash is already in the
        // store, skip silently. This makes `ingest_*` idempotent
        // under re-crawl.
        let existing: Option<i64> = self
            .conn
            .prepare_cached("SELECT 1 FROM chunk WHERE content_hash = ? LIMIT 1")?
            .query_row(params![hash.as_bytes()], |r| r.get(0))
            .ok();
        if existing.is_some() {
            return Ok(false);
        }

        let now = Utc::now().timestamp();
        // Run the chunk-row INSERT and the FTS5 INSERT inside the
        // same transaction so a primary-key collision on
        // ``chunk_id`` (re-crawl of an existing chunk_id with newly
        // *different* text — the content-hash dedup at line 346
        // already caught the same-text case) cannot leave the FTS5
        // index pointing at a row that wasn't actually written. The
        // ``INSERT OR IGNORE`` returns a row count of 0 on the PK
        // collision and we then bail out with ``Ok(false)`` *before*
        // touching ``chunk_fts``, so the index never gains an
        // orphan entry and the caller's "inserted" counter stays
        // accurate.
        let tx = self.conn.unchecked_transaction()?;
        let inserted = tx
            .prepare_cached(
                r#"INSERT OR IGNORE INTO chunk
                  (chunk_id, episode_id, section_heading, chunk_text,
                   token_count, citation_anchor, content_hash, created_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)"#,
            )?
            .execute(params![
                chunk.chunk_id,
                chunk.episode_id,
                chunk.section_heading,
                chunk.chunk_text,
                chunk.token_count as i64,
                chunk.citation_anchor,
                hash.as_bytes(),
                now,
            ])?;
        if inserted == 0 {
            // Same ``chunk_id``, different text: the content-hash
            // dedup at line 346 did *not* short-circuit (new text
            // → new hash), but the unique-PK trigger on
            // ``chunk.chunk_id`` rejected the row. Skip the FTS5
            // insert and report ``Ok(false)`` so the caller's
            // stats reflect the no-op. The transaction's drop is
            // a rollback, which is what we want here.
            return Ok(false);
        }

        tx.prepare_cached(
            r#"INSERT INTO chunk_fts (chunk_text, chunk_id, episode_id)
                  VALUES (?, ?, ?)"#,
        )?
        .execute(params![chunk.chunk_text, chunk.chunk_id, chunk.episode_id])?;
        tx.commit()?;
        Ok(true)
    }

    /// Append a row to the governance log. Used both by the
    /// happy-path episode ingest and by the rights-gate rejection
    /// path. Re-inserts of the same `(episode_id, content_hash)`
    /// produce a fresh row (the log is a cadence trail, not a
    /// uniqueness index).
    pub fn append_governance(&self, entry: &GovernanceEntry) -> Result<()> {
        let hash_bytes = hex::decode(&entry.content_hash).map_err(|_| {
            PackError::Invariant(format!(
                "governance entry for {} has non-hex content_hash {}",
                entry.episode_id, entry.content_hash
            ))
        })?;
        self.conn
            .prepare_cached(
                r#"INSERT INTO governance_log
                  (episode_id, rights_code, ingestion_date, content_hash, deprecated)
                  VALUES (?, ?, ?, ?, ?)"#,
            )?
            .execute(params![
                entry.episode_id,
                entry.rights_code,
                entry.ingestion_date,
                hash_bytes,
                if entry.deprecated { 1 } else { 0 },
            ])?;
        Ok(())
    }

    /// Bulk-ingest a publisher's metadata + chunks JSONL pair.
    pub fn ingest_publisher_jsonl(
        &self,
        publisher_id: &str,
        metadata_jsonl: &Path,
        chunks_jsonl: &Path,
    ) -> Result<IngestStats> {
        let mut stats = IngestStats::default();
        let mut rejected_episode_ids = std::collections::HashSet::new();

        // ---- Episodes ----------------------------------------------
        if metadata_jsonl.exists() {
            let f = std::fs::File::open(metadata_jsonl)?;
            for line in std::io::BufReader::new(f).lines() {
                let line = line?;
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                stats.episodes_seen += 1;
                let episode: Episode = serde_json::from_str(line)?;
                if episode.publisher != publisher_id {
                    return Err(PackError::Invariant(format!(
                        "episode {} has publisher={} but file is for {}",
                        episode.episode_id, episode.publisher, publisher_id
                    )));
                }
                match self.ingest_episode(&episode) {
                    Ok(true) => stats.episodes_inserted += 1,
                    Ok(false) => stats.episodes_skipped_existing += 1,
                    Err(PackError::RightsGateRefused { episode_id, .. }) => {
                        stats.episodes_rejected_rights += 1;
                        rejected_episode_ids.insert(episode_id);
                    }
                    Err(e) => return Err(e),
                }
            }
        }

        // ---- Chunks ------------------------------------------------
        if chunks_jsonl.exists() {
            let f = std::fs::File::open(chunks_jsonl)?;
            for line in std::io::BufReader::new(f).lines() {
                let line = line?;
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                stats.chunks_seen += 1;
                let chunk: Chunk = serde_json::from_str(line)?;
                if rejected_episode_ids.contains(&chunk.episode_id) {
                    stats.chunks_skipped_rights += 1;
                    continue;
                }
                if self.ingest_chunk(&chunk)? {
                    stats.chunks_inserted += 1;
                } else {
                    stats.chunks_skipped_dedup += 1;
                }
            }
        }

        Ok(stats)
    }

    /// Bulk-ingest every publisher under a packs root. The root is
    /// expected to be the project's `packs/` directory.
    pub fn ingest_jsonl_files(&self, packs_root: &Path) -> Result<IngestReport> {
        let mut report = IngestReport::default();
        let metadata_dir = packs_root.join("metadata");
        let chunks_dir = packs_root.join("chunks");
        if !metadata_dir.is_dir() {
            return Ok(report);
        }
        let mut entries: Vec<_> = std::fs::read_dir(&metadata_dir)?
            .filter_map(|r| r.ok())
            .filter(|e| e.path().extension().is_some_and(|x| x == "jsonl"))
            .collect();
        entries.sort_by_key(|e| e.path());
        for entry in entries {
            let metadata_path = entry.path();
            let publisher = metadata_path
                .file_stem()
                .and_then(|s| s.to_str())
                .ok_or_else(|| {
                    PackError::Invariant(format!("bad jsonl filename: {metadata_path:?}"))
                })?
                .to_string();
            let chunks_path = chunks_dir.join(format!("{publisher}.jsonl"));
            let stats = self.ingest_publisher_jsonl(&publisher, &metadata_path, &chunks_path)?;
            report.by_publisher.insert(publisher, stats);
        }
        Ok(report)
    }

    /// Count rows in the `chunk` table. Useful for the export
    /// manifest and for tests.
    pub fn chunk_count(&self) -> Result<u64> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM chunk", [], |r| r.get(0))?;
        Ok(n as u64)
    }

    /// Count rows in the `episode` table.
    pub fn episode_count(&self) -> Result<u64> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM episode", [], |r| r.get(0))?;
        Ok(n as u64)
    }
}

/// Canonicalise chunk text so byte-for-byte equivalent re-crawls
/// hash identically:
///
/// * Line endings normalised to `\n`.
/// * Tab characters expanded to a single space.
/// * Trailing whitespace stripped from every line.
/// * Leading + trailing blank lines collapsed.
///
/// Returns an owned `String` so the original chunk text is left
/// unchanged (it goes into the row verbatim — we want the canonical
/// form only for hashing).
fn canonicalise_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for line in s.replace("\r\n", "\n").replace('\r', "\n").split('\n') {
        let line = line.replace('\t', " ");
        let trimmed = line.trim_end();
        out.push_str(trimmed);
        out.push('\n');
    }
    out.trim().to_string()
}

// Tiny inline hex impl. Kept here rather than pulling the `hex`
// crate so the workspace has one fewer transitive dependency.
mod hex {
    pub fn decode(s: &str) -> Result<Vec<u8>, &'static str> {
        if s.len() % 2 != 0 {
            return Err("odd-length hex");
        }
        let mut out = Vec::with_capacity(s.len() / 2);
        let bytes = s.as_bytes();
        for chunk in bytes.chunks(2) {
            let hi = from_hex_char(chunk[0])?;
            let lo = from_hex_char(chunk[1])?;
            out.push((hi << 4) | lo);
        }
        Ok(out)
    }
    fn from_hex_char(b: u8) -> Result<u8, &'static str> {
        match b {
            b'0'..=b'9' => Ok(b - b'0'),
            b'a'..=b'f' => Ok(b - b'a' + 10),
            b'A'..=b'F' => Ok(b - b'A' + 10),
            _ => Err("bad hex char"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metadata::{ChunkingPolicy, DEFAULT_CHUNKING};

    fn sample_episode(id: &str, rights: &str) -> Episode {
        Episode {
            episode_id: id.into(),
            publisher: "acquired".into(),
            series: "flagship".into(),
            title: "Costco".into(),
            host: vec!["Ben Gilbert".into(), "David Rosenthal".into()],
            guest: vec![],
            publication_date: "2021-09-21".into(),
            country_region: vec!["US".into(), "global".into()],
            industry_tags: vec!["retail".into()],
            function_tags: vec!["strategy".into()],
            business_model_tags: vec!["B2C".into()],
            source_type: "podcast_transcript_html".into(),
            language: "en".into(),
            primary_url: "https://www.acquired.fm/episodes/costco".into(),
            asset_urls: vec![],
            rights_code: rights.into(),
            rights_summary: "Free-access copyrighted; show notes + transcript on official site."
                .into(),
            credibility_notes: "First-party publisher; long-form authoritative.".into(),
            summary: "Costco's strategic moats.".into(),
            chunking_policy: Some(ChunkingPolicy {
                target_tokens: 700,
                overlap_tokens: 120,
            }),
        }
    }

    #[test]
    fn schema_applies_and_round_trips_episode() {
        let store = PackStore::open_in_memory().expect("open");
        let ep = sample_episode("acquired_flagship_costco", "free_access_copyrighted");
        let inserted = store.ingest_episode(&ep).expect("ingest");
        assert!(inserted);
        assert_eq!(store.episode_count().unwrap(), 1);

        // Idempotent re-ingest.
        let inserted2 = store.ingest_episode(&ep).expect("ingest again");
        assert!(!inserted2);
        assert_eq!(store.episode_count().unwrap(), 1);
    }

    #[test]
    fn rights_gate_rejects_unknown_and_logs() {
        let store = PackStore::open_in_memory().expect("open");
        let ep = sample_episode("acquired_flagship_unknown", "unknown");
        let err = store.ingest_episode(&ep).expect_err("should reject");
        match err {
            PackError::RightsGateRefused { rights_code, .. } => {
                assert_eq!(rights_code, "unknown");
            }
            other => panic!("unexpected: {other:?}"),
        }
        // Audit trail must record the rejection.
        let n: i64 = store
            .conn
            .query_row(
                "SELECT COUNT(*) FROM governance_log WHERE deprecated = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn chunk_dedup_on_content_hash() {
        let store = PackStore::open_in_memory().expect("open");
        let ep = sample_episode("acquired_flagship_costco", "free_access_copyrighted");
        store.ingest_episode(&ep).unwrap();
        let c = Chunk {
            chunk_id: "acquired_flagship_costco#0001".into(),
            episode_id: ep.episode_id.clone(),
            token_count: 12,
            section_heading: Some("Introduction".into()),
            chunk_text: "BEN: Costco's flywheel works because…".into(),
            citation_anchor: format!("{}#section-1", ep.primary_url),
        };
        assert!(store.ingest_chunk(&c).unwrap());
        // Same text, different chunk_id => content_hash collides,
        // dedup skips.
        let c2 = Chunk {
            chunk_id: "acquired_flagship_costco#0002".into(),
            chunk_text: "BEN: Costco's flywheel works because…".into(),
            ..c.clone()
        };
        assert!(!store.ingest_chunk(&c2).unwrap());
        assert_eq!(store.chunk_count().unwrap(), 1);
    }

    #[test]
    fn default_chunking_constant_round_trips() {
        // Make sure the documented default does not drift.
        assert_eq!(DEFAULT_CHUNKING.target_tokens, 700);
        assert_eq!(DEFAULT_CHUNKING.overlap_tokens, 120);
    }

    #[test]
    fn append_only_trigger_rejects_update() {
        let store = PackStore::open_in_memory().expect("open");
        let ep = sample_episode("acquired_flagship_costco", "free_access_copyrighted");
        store.ingest_episode(&ep).unwrap();
        let res = store
            .conn
            .execute("UPDATE episode SET title = 'changed'", []);
        assert!(res.is_err(), "trigger should reject UPDATE");
    }

    #[test]
    fn duplicate_chunk_id_with_different_text_does_not_leave_fts_orphan() {
        // Regression: a re-crawl that produces the same ``chunk_id``
        // with newly *different* text used to fall through the
        // content-hash dedup (new text → new hash) but get rejected
        // by ``INSERT OR IGNORE INTO chunk`` (PK collision) — and
        // the FTS5 insert ran anyway, leaving a phantom entry that
        // pointed at stale text.
        let store = PackStore::open_in_memory().expect("open");
        let ep = sample_episode("acquired_flagship_costco", "free_access_copyrighted");
        store.ingest_episode(&ep).unwrap();
        let original = Chunk {
            chunk_id: "acquired_flagship_costco#0001".into(),
            episode_id: ep.episode_id.clone(),
            token_count: 12,
            section_heading: Some("Introduction".into()),
            chunk_text: "BEN: Costco's flywheel works because of the membership.".into(),
            citation_anchor: format!("{}#section-1", ep.primary_url),
        };
        assert!(store.ingest_chunk(&original).unwrap());

        // Same ``chunk_id``, edited text (different hash).
        let edited = Chunk {
            chunk_text: "BEN: Costco's flywheel works because of cheap-rotisserie scale.".into(),
            ..original.clone()
        };
        assert!(
            !store.ingest_chunk(&edited).unwrap(),
            "second ingest must report no-op",
        );

        // Exactly one chunk row, exactly one FTS5 row, and the FTS5
        // row points at the original text — not at the edit.
        assert_eq!(store.chunk_count().unwrap(), 1);
        let fts_count: i64 = store
            .conn
            .query_row("SELECT COUNT(*) FROM chunk_fts", [], |r| r.get(0))
            .unwrap();
        assert_eq!(fts_count, 1, "FTS5 must not have an orphan row");
        let fts_text: String = store
            .conn
            .query_row(
                "SELECT chunk_text FROM chunk_fts WHERE chunk_id = ?",
                params![original.chunk_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            fts_text, original.chunk_text,
            "FTS5 row must still point at the original text",
        );
    }
}
