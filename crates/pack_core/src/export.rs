//! Compact `.pack` file format. One file ships the entire on-device
//! knowledge pack: chunk text, metadata, FTS5 index, governance
//! manifest, and a header that tells the agent how to mount it.
//!
//! Layout:
//!
//! ```text
//! +-----------------------+
//! | MAGIC ("SNMDM")       |   5 bytes
//! +-----------------------+
//! | u8 pack_format_version|   1 byte
//! +-----------------------+
//! | u64 manifest_len      |   8 bytes, little-endian
//! +-----------------------+
//! |   MANIFEST (JSON)     |   manifest_len bytes
//! +-----------------------+
//! | u64 db_len            |   8 bytes, little-endian
//! +-----------------------+
//! | zstd(SQLite db)       |   db_len bytes
//! +-----------------------+
//! ```
//!
//! The magic and the version byte are split: a future v2 pack will
//! still present the `SNMDM` magic and surface a more informative
//! [`PackError::UnsupportedPackVersion`] rather than the generic
//! [`PackError::BadMagic`]. Length headers are `u64` so packs are
//! not silently bounded to 4 GiB.
//!
//! The manifest carries:
//!
//! * Pack format version.
//! * Build timestamp (Unix epoch seconds).
//! * Counts of episodes, chunks, publishers.
//! * The full [`PackHeader`] (pack-format version, schema version,
//!   ranker weights).
//! * BLAKE3 hash of the zstd-compressed SQLite blob — verified on
//!   load to catch corrupted packs before they hit SQLCipher.
//!
//! The SQLite database inside is the same schema the live ingest
//! writes to. Decompressing the pack restores a runnable
//! `PackStore` — see [`PackReader::extract_to`].

use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;

use chrono::Utc;
use serde::{Deserialize, Serialize};

use crate::error::{PackError, Result};
use crate::ingest::PackStore;
use crate::search::RankWeights;
use crate::{PACK_MAGIC, SCHEMA_VERSION};

/// Pack-file format version. Stamped onto every `.pack` manifest
/// and written immediately after the [`PACK_MAGIC`] bytes in the
/// file header. Independent of [`SCHEMA_VERSION`] — bumped only
/// when the framing layout itself changes (e.g. a new section is
/// added after the SQLite blob).
pub const PACK_FORMAT_VERSION: u8 = 1;

/// Default zstd compression level for the SQLite blob. Level 19 hits
/// a strong size/CPU trade-off on the SQLite text blobs the pack
/// carries — anything above 21 saves <1% size for >10x build time.
pub const DEFAULT_ZSTD_LEVEL: i32 = 19;

/// Header summarising the pack-format invariants. Stored inside the
/// manifest so the agent can refuse incompatible packs at mount
/// time.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PackHeader {
    /// Pack-file framing version. Must equal
    /// [`PACK_FORMAT_VERSION`].
    pub pack_format_version: u8,
    /// SQLite schema version inside the pack. Must equal
    /// [`SCHEMA_VERSION`].
    pub schema_version: i32,
    /// Default rank weights the on-device agent should use when no
    /// per-query override is supplied. Set at build time; the
    /// agent may still override per-query.
    pub default_rank_weights: SerializableWeights,
    /// zstd level the SQLite blob was compressed at. Recorded for
    /// diagnostics; not required at decompression time.
    pub zstd_level: i32,
}

/// JSON-friendly view of [`RankWeights`]. The `RankWeights` struct
/// derives `Copy` + `PartialEq` (not `Serialize`) so the engine
/// can stay a thin algorithmic type while the pack header still
/// captures the build-time values.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SerializableWeights {
    pub bm25: f64,
    pub semantic: f64,
    pub tag_boost: f64,
}

impl From<RankWeights> for SerializableWeights {
    fn from(w: RankWeights) -> Self {
        Self {
            bm25: w.bm25,
            semantic: w.semantic,
            tag_boost: w.tag_boost,
        }
    }
}

impl From<SerializableWeights> for RankWeights {
    fn from(s: SerializableWeights) -> Self {
        Self {
            bm25: s.bm25,
            semantic: s.semantic,
            tag_boost: s.tag_boost,
        }
    }
}

/// Manifest written to the beginning of every pack file.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PackManifest {
    pub header: PackHeader,
    /// Build timestamp (Unix epoch seconds).
    pub built_at: i64,
    /// Number of distinct publishers in the pack.
    pub publisher_count: u64,
    /// Number of episodes in the pack.
    pub episode_count: u64,
    /// Number of chunks in the pack.
    pub chunk_count: u64,
    /// Hex-encoded BLAKE3 checksum of the zstd-compressed SQLite
    /// blob. Verified on load by [`PackReader::open`].
    pub blob_blake3: String,
    /// Per-publisher chunk counts. Useful for debugging / coverage
    /// reports.
    pub publishers: Vec<PublisherManifest>,
    /// Free-text build notes — surfaced verbatim. The CLI sets this
    /// to the git SHA + crawl date by default.
    #[serde(default)]
    pub build_notes: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PublisherManifest {
    pub publisher: String,
    pub episode_count: u64,
    pub chunk_count: u64,
}

/// Build a `.pack` file from a live [`PackStore`].
///
/// The builder vacuums the in-memory database into a temp file,
/// reads it as a single blob, zstd-compresses it, and writes the
/// canonical framing.
pub struct PackBuilder<'a> {
    store: &'a PackStore,
    weights: RankWeights,
    zstd_level: i32,
    build_notes: String,
}

impl<'a> PackBuilder<'a> {
    /// New builder with default weights and compression level.
    pub fn new(store: &'a PackStore) -> Self {
        Self {
            store,
            weights: RankWeights::default(),
            zstd_level: DEFAULT_ZSTD_LEVEL,
            build_notes: String::new(),
        }
    }

    /// Override the default rank weights stamped into the header.
    pub fn with_rank_weights(mut self, weights: RankWeights) -> Self {
        self.weights = weights;
        self
    }

    /// Override the zstd compression level (default
    /// [`DEFAULT_ZSTD_LEVEL`]).
    pub fn with_zstd_level(mut self, level: i32) -> Self {
        self.zstd_level = level;
        self
    }

    /// Attach build notes to the manifest (typically `git rev-parse
    /// HEAD` + the crawl date).
    pub fn with_build_notes(mut self, notes: impl Into<String>) -> Self {
        self.build_notes = notes.into();
        self
    }

    /// Build the pack to `output_path`. Overwrites any existing
    /// file at that path.
    pub fn build_to<P: AsRef<Path>>(&self, output_path: P) -> Result<PackManifest> {
        let path = output_path.as_ref();

        // 1. VACUUM INTO a child of a private tempdir. The tempdir
        //    is owned for the duration of this call (its `Drop` cleans
        //    the directory and everything inside it on success or
        //    panic), and we hand SQLite a *non-existent* path inside
        //    that directory so there's no race window where another
        //    process could reclaim the inode between `drop(tmp)` and
        //    `VACUUM INTO`. The directory mode is the OS default for
        //    `mkdtemp` (0700 on Unix), so a sibling process can't
        //    plant a symlink at the chosen path either.
        let tmp_dir = tempfile::Builder::new().prefix("sn-mdm-pack-").tempdir()?;
        let tmp_path = tmp_dir.path().join("snapshot.sqlite");

        let escaped = tmp_path.to_string_lossy().replace('\'', "''");
        self.store
            .connection()
            .execute_batch(&format!("VACUUM INTO '{escaped}'"))?;

        // 2. Read the snapshot and zstd-compress it.
        let mut raw_db = Vec::new();
        File::open(&tmp_path)?.read_to_end(&mut raw_db)?;
        let compressed = zstd::stream::encode_all(std::io::Cursor::new(&raw_db), self.zstd_level)?;
        drop(tmp_dir); // best-effort cleanup; tempdir Drop also unlinks.

        // 3. Build the manifest.
        let publishers = self.collect_publisher_manifests()?;
        let manifest = PackManifest {
            header: PackHeader {
                pack_format_version: PACK_FORMAT_VERSION,
                schema_version: SCHEMA_VERSION,
                default_rank_weights: self.weights.into(),
                zstd_level: self.zstd_level,
            },
            built_at: Utc::now().timestamp(),
            publisher_count: publishers.len() as u64,
            episode_count: self.store.episode_count()?,
            chunk_count: self.store.chunk_count()?,
            blob_blake3: blake3::hash(&compressed).to_hex().to_string(),
            publishers,
            build_notes: self.build_notes.clone(),
        };
        let manifest_bytes = serde_json::to_vec(&manifest)?;

        // 4. Write the framed pack. Lengths are `u64` so packs are
        //    not silently bounded to 4 GiB.
        let mut out = std::fs::File::create(path)?;
        out.write_all(&PACK_MAGIC)?;
        out.write_all(&[PACK_FORMAT_VERSION])?;
        out.write_all(&(manifest_bytes.len() as u64).to_le_bytes())?;
        out.write_all(&manifest_bytes)?;
        out.write_all(&(compressed.len() as u64).to_le_bytes())?;
        out.write_all(&compressed)?;
        out.flush()?;
        Ok(manifest)
    }

    fn collect_publisher_manifests(&self) -> Result<Vec<PublisherManifest>> {
        let mut stmt = self.store.connection().prepare(
            r#"SELECT
                   e.publisher,
                   COUNT(DISTINCT e.episode_id) AS ep_count,
                   COUNT(c.chunk_id)            AS chunk_count
               FROM episode e
               LEFT JOIN chunk c ON c.episode_id = e.episode_id
               GROUP BY e.publisher
               ORDER BY e.publisher"#,
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(PublisherManifest {
                publisher: r.get(0)?,
                episode_count: r.get::<_, i64>(1)? as u64,
                chunk_count: r.get::<_, i64>(2)? as u64,
            })
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }
}

/// Read a `.pack` file built by [`PackBuilder`]. Verifies the
/// header, manifest checksum, and schema version before returning.
#[derive(Debug)]
pub struct PackReader {
    /// Manifest as deserialised from the pack header.
    pub manifest: PackManifest,
    /// Raw zstd-compressed SQLite blob. Use
    /// [`PackReader::extract_to`] to write the decompressed
    /// database to disk.
    compressed_db: Vec<u8>,
}

impl PackReader {
    /// Read and verify a `.pack` file from `path`.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        let mut bytes = Vec::new();
        File::open(path.as_ref())?.read_to_end(&mut bytes)?;
        Self::from_bytes(bytes)
    }

    /// Parse and verify an in-memory pack blob.
    ///
    /// Every byte read from `bytes` is bounds-checked. A length
    /// header that does not fit within the buffer surfaces as
    /// [`PackError::TruncatedPack`] (with the failing field name
    /// and offsets) rather than panicking. Magic / version / schema
    /// / checksum mismatches all surface as their respective
    /// [`PackError`] variants.
    pub fn from_bytes(bytes: Vec<u8>) -> Result<Self> {
        // Minimum framing: magic (5) + version (1) + manifest_len
        // (8) + db_len (8) = 22 bytes. A pack smaller than that is
        // structurally invalid.
        const MIN_HEADER_BYTES: usize = PACK_MAGIC.len() + 1 + 8 + 8;
        if bytes.len() < MIN_HEADER_BYTES {
            return Err(PackError::TruncatedPack {
                field: "header",
                offset: 0,
                needed: MIN_HEADER_BYTES,
                available: bytes.len(),
            });
        }
        if bytes[..PACK_MAGIC.len()] != PACK_MAGIC {
            return Err(PackError::BadMagic);
        }
        let format_version = bytes[PACK_MAGIC.len()];
        if format_version != PACK_FORMAT_VERSION {
            return Err(PackError::UnsupportedPackVersion {
                found: format_version,
            });
        }

        let mut cursor = PACK_MAGIC.len() + 1;
        let manifest_len = read_u64_field(&bytes, &mut cursor, "manifest_len")?;
        let manifest_bytes = read_slice(&bytes, &mut cursor, manifest_len, "manifest")?;
        let manifest: PackManifest = serde_json::from_slice(manifest_bytes)?;

        let db_len = read_u64_field(&bytes, &mut cursor, "db_len")?;
        let compressed_db = read_slice(&bytes, &mut cursor, db_len, "db_blob")?.to_vec();

        // Manifest checksum.
        let computed = blake3::hash(&compressed_db).to_hex().to_string();
        if computed != manifest.blob_blake3 {
            return Err(PackError::ChecksumMismatch {
                claimed: manifest.blob_blake3.clone(),
                computed,
            });
        }
        if manifest.header.schema_version > SCHEMA_VERSION {
            return Err(PackError::IncompatibleSchema {
                found: manifest.header.schema_version,
                expected: SCHEMA_VERSION,
            });
        }

        Ok(Self {
            manifest,
            compressed_db,
        })
    }

    /// Decompress the SQLite blob and write it to `path`. After
    /// this, callers can re-open the file with
    /// [`PackStore::open`].
    pub fn extract_to<P: AsRef<Path>>(&self, path: P) -> Result<()> {
        let mut decoder = zstd::stream::Decoder::new(std::io::Cursor::new(&self.compressed_db))?;
        let mut decompressed = Vec::new();
        decoder.read_to_end(&mut decompressed)?;
        std::fs::write(path, decompressed)?;
        Ok(())
    }
}

/// Read a little-endian `u64` length field from `bytes[*cursor..*cursor + 8]`
/// and advance `*cursor`. Returns [`PackError::TruncatedPack`] if the
/// 8-byte window does not fit within `bytes`. The returned `usize`
/// is the length the caller can pass straight to [`read_slice`] on
/// 64-bit platforms; on 32-bit platforms we additionally guard
/// against the `usize` cast truncating a value that wouldn't fit
/// in `usize::MAX` anyway.
fn read_u64_field(bytes: &[u8], cursor: &mut usize, field: &'static str) -> Result<usize> {
    let end = cursor.checked_add(8).ok_or(PackError::TruncatedPack {
        field,
        offset: *cursor,
        needed: 8,
        available: bytes.len().saturating_sub(*cursor),
    })?;
    if end > bytes.len() {
        return Err(PackError::TruncatedPack {
            field,
            offset: *cursor,
            needed: 8,
            available: bytes.len().saturating_sub(*cursor),
        });
    }
    let value = u64::from_le_bytes(bytes[*cursor..end].try_into().unwrap());
    *cursor = end;
    usize::try_from(value).map_err(|_| PackError::TruncatedPack {
        field,
        offset: end,
        needed: usize::MAX, // value doesn't fit in usize on this target
        available: bytes.len().saturating_sub(end),
    })
}

/// Take `len` bytes from `bytes[*cursor..]` and advance `*cursor`.
/// Returns [`PackError::TruncatedPack`] if the requested window
/// does not fit within `bytes`. Pure bounds-checking; the caller
/// is responsible for treating the borrowed slice as valid input.
fn read_slice<'a>(
    bytes: &'a [u8],
    cursor: &mut usize,
    len: usize,
    field: &'static str,
) -> Result<&'a [u8]> {
    let end = cursor.checked_add(len).ok_or(PackError::TruncatedPack {
        field,
        offset: *cursor,
        needed: len,
        available: bytes.len().saturating_sub(*cursor),
    })?;
    if end > bytes.len() {
        return Err(PackError::TruncatedPack {
            field,
            offset: *cursor,
            needed: len,
            available: bytes.len().saturating_sub(*cursor),
        });
    }
    let out = &bytes[*cursor..end];
    *cursor = end;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metadata::{Chunk, Episode};

    fn seed(store: &PackStore) {
        let ep = Episode {
            episode_id: "acquired_flagship_costco".into(),
            publisher: "acquired".into(),
            series: "flagship".into(),
            title: "Costco".into(),
            host: vec!["Ben Gilbert".into()],
            guest: vec![],
            publication_date: "2021-09-21".into(),
            country_region: vec!["US".into()],
            industry_tags: vec!["retail".into()],
            function_tags: vec!["strategy".into()],
            business_model_tags: vec!["B2C".into()],
            source_type: "podcast_transcript_html".into(),
            language: "en".into(),
            primary_url: "https://www.acquired.fm/episodes/costco".into(),
            asset_urls: vec![],
            rights_code: "free_access_copyrighted".into(),
            rights_summary: "Free-access copyrighted.".into(),
            credibility_notes: "First-party.".into(),
            summary: "Costco's strategic moats.".into(),
            chunking_policy: None,
        };
        store.ingest_episode(&ep).unwrap();
        for (i, text) in [
            "BEN: Costco's flywheel works because of the membership fee.",
            "DAVID: The treasure-hunt format keeps members engaged.",
        ]
        .iter()
        .enumerate()
        {
            store
                .ingest_chunk(&Chunk {
                    chunk_id: format!("acquired_flagship_costco#{i:04}"),
                    episode_id: ep.episode_id.clone(),
                    token_count: text.split_whitespace().count(),
                    section_heading: Some(format!("Section {i}")),
                    chunk_text: (*text).into(),
                    citation_anchor: format!("{}#section-{i}", ep.primary_url),
                })
                .unwrap();
        }
    }

    #[test]
    fn round_trip_pack_file() {
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        let tmp = tempfile::Builder::new().suffix(".pack").tempfile().unwrap();
        let pack_path = tmp.path().to_path_buf();
        let manifest = PackBuilder::new(&store)
            .with_build_notes("test-build")
            .build_to(&pack_path)
            .expect("build pack");
        assert_eq!(manifest.episode_count, 1);
        assert_eq!(manifest.chunk_count, 2);
        assert_eq!(manifest.publishers.len(), 1);

        let reader = PackReader::open(&pack_path).expect("open pack");
        assert_eq!(reader.manifest.episode_count, 1);
        assert_eq!(reader.manifest.chunk_count, 2);
        assert_eq!(reader.manifest.header.schema_version, SCHEMA_VERSION);
        assert_eq!(reader.manifest.build_notes, "test-build");

        // Decompress + reopen as a live store.
        let tmp_db = tempfile::Builder::new()
            .suffix(".sqlite")
            .tempfile()
            .unwrap();
        let db_path = tmp_db.path().to_path_buf();
        drop(tmp_db);
        reader.extract_to(&db_path).expect("extract");
        let reopened = PackStore::open(&db_path, "").expect("reopen");
        assert_eq!(reopened.episode_count().unwrap(), 1);
        assert_eq!(reopened.chunk_count().unwrap(), 2);
    }

    #[test]
    fn checksum_mismatch_is_detected() {
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        let tmp = tempfile::Builder::new().suffix(".pack").tempfile().unwrap();
        let pack_path = tmp.path().to_path_buf();
        PackBuilder::new(&store)
            .build_to(&pack_path)
            .expect("build pack");
        // Flip a byte deep in the compressed blob.
        let mut bytes = std::fs::read(&pack_path).unwrap();
        let len = bytes.len();
        bytes[len - 4] ^= 0xff;
        std::fs::write(&pack_path, &bytes).unwrap();
        match PackReader::open(&pack_path) {
            Err(PackError::ChecksumMismatch { .. }) => {}
            other => panic!("expected checksum error, got {other:?}"),
        }
    }

    #[test]
    fn truncated_input_does_not_panic() {
        // Empty input — below minimum header size.
        match PackReader::from_bytes(Vec::new()) {
            Err(PackError::TruncatedPack { field, .. }) => assert_eq!(field, "header"),
            other => panic!("expected truncated-pack error, got {other:?}"),
        }
        // Magic-only — below minimum header size.
        match PackReader::from_bytes(b"SNMDM".to_vec()) {
            Err(PackError::TruncatedPack { field, .. }) => assert_eq!(field, "header"),
            other => panic!("expected truncated-pack error, got {other:?}"),
        }
    }

    #[test]
    fn oversized_length_header_returns_truncated_error() {
        // Build a header that claims an absurd manifest length.
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&PACK_MAGIC);
        bytes.push(PACK_FORMAT_VERSION);
        bytes.extend_from_slice(&u64::MAX.to_le_bytes()); // manifest_len = u64::MAX
        bytes.extend_from_slice(&0u64.to_le_bytes()); // db_len = 0
        match PackReader::from_bytes(bytes) {
            Err(PackError::TruncatedPack { field, .. }) => assert_eq!(field, "manifest"),
            other => panic!("expected truncated-pack error, got {other:?}"),
        }
    }

    #[test]
    fn bad_magic_is_detected() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(b"BADMG"); // 5 bytes != SNMDM
        bytes.push(PACK_FORMAT_VERSION);
        bytes.extend_from_slice(&0u64.to_le_bytes());
        bytes.extend_from_slice(&0u64.to_le_bytes());
        match PackReader::from_bytes(bytes) {
            Err(PackError::BadMagic) => {}
            other => panic!("expected bad-magic error, got {other:?}"),
        }
    }

    #[test]
    fn unsupported_version_is_detected() {
        // Same magic, but version byte = 99 (future format).
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&PACK_MAGIC);
        bytes.push(99);
        bytes.extend_from_slice(&0u64.to_le_bytes());
        bytes.extend_from_slice(&0u64.to_le_bytes());
        match PackReader::from_bytes(bytes) {
            Err(PackError::UnsupportedPackVersion { found }) => assert_eq!(found, 99),
            other => panic!("expected unsupported-version error, got {other:?}"),
        }
    }
}
