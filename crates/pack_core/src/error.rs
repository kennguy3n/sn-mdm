//! Error type for the `pack_core` crate.
//!
//! All fallible operations return [`Result<T>`], an alias for
//! `std::result::Result<T, PackError>`. Errors are surfaced verbatim
//! to callers — the crate never `unwrap()`s on user-controlled input.

use std::io;

/// Error variants emitted by `pack_core`.
#[derive(Debug, thiserror::Error)]
pub enum PackError {
    /// Underlying SQLite / SQLCipher error.
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    /// JSON parse / serialisation error from a metadata or chunk
    /// JSONL line.
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    /// I/O error reading or writing pack files.
    #[error("io error: {0}")]
    Io(#[from] io::Error),

    /// Schema version on disk is incompatible with the binary.
    /// Returned by [`crate::ingest::PackStore::open`] when the
    /// on-disk `PRAGMA user_version` is greater than
    /// [`crate::SCHEMA_VERSION`] (downgrades are not supported).
    #[error("incompatible schema version: on-disk v{found}, binary v{expected}")]
    IncompatibleSchema { found: i32, expected: i32 },

    /// `.pack` file header did not start with [`crate::PACK_MAGIC`].
    #[error("pack header magic mismatch (expected SNMDM\\x01)")]
    BadMagic,

    /// `.pack` file declared a pack-format version this binary does
    /// not understand.
    #[error("unsupported pack format version: {found}")]
    UnsupportedPackVersion { found: u8 },

    /// `.pack` file's BLAKE3 manifest checksum did not match the
    /// computed value over its contents.
    #[error("pack checksum mismatch: manifest claims {claimed}, computed {computed}")]
    ChecksumMismatch { claimed: String, computed: String },

    /// Rights gate refused to ingest an episode. The gate is run
    /// inside [`crate::ingest::PackStore::ingest_episode`] and only
    /// admits rights codes from the configured allowlist.
    #[error(
        "rights gate refused episode {episode_id}: rights_code={rights_code} not in allowlist"
    )]
    RightsGateRefused {
        episode_id: String,
        rights_code: String,
    },

    /// Caller-supplied data violated a domain invariant.
    #[error("invariant violation: {0}")]
    Invariant(String),
}

/// Convenience `Result` alias used throughout the crate.
pub type Result<T> = std::result::Result<T, PackError>;
