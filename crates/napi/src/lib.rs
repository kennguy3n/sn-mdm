//! `pack_napi` — N-API addon bridging [`pack_core`] to Node.js
//! consumers (Electron desktop hosts, Node CLI tooling, future
//! browser-side WASM ports via the same JSON envelope).
//!
//! The crate exposes a small, handle-based surface:
//!
//! 1. [`open_pack`] — open and verify a ``.pack`` file. Decompresses
//!    the embedded SQLite blob into a private tempdir, opens it as
//!    a [`pack_core::PackStore`], and returns a 64-bit handle the
//!    host must pass back into every subsequent call.
//! 2. [`search`] — run a query against the store identified by a
//!    handle. Returns hits as a JSON array.
//! 3. [`close_pack`] — drop the open store. Removes the registry
//!    entry; the [`tempfile::TempDir`] held inside the entry is
//!    dropped with it, deleting the extracted SQLite file.
//!
//! The pure-Rust API in this file is the canonical surface — every
//! ``#[napi]``-annotated wrapper in [`bindings`] is a thin adapter
//! that converts JS values into the Rust types and forwards the
//! call. The split lets the crate's unit tests exercise the same
//! code paths that JavaScript callers see at runtime without going
//! through the Node bridge.
//!
//! ## Concurrency model
//!
//! The handle registry is an [`std::sync::RwLock`] over a
//! [`std::collections::HashMap`]. Each entry is an
//! [`std::sync::Arc`] of a [`std::sync::Mutex`]-wrapped
//! [`pack_core::PackStore`]: lookups can proceed concurrently;
//! queries against the *same* store serialise through the mutex
//! because [`rusqlite::Connection`] is ``Send`` but not ``Sync``.
//! Queries against *different* stores run in parallel.
//!
//! Closing a handle removes its entry under the registry write
//! lock; any concurrent [`search`] holding an outstanding ``Arc``
//! clone of the same store completes against that ``Arc`` and the
//! store is dropped on the last clone — exactly the lifetime
//! contract Node hosts expect from a ``close``-then-GC sequence.

#![deny(missing_docs)]

pub mod bindings;
pub mod error;
pub mod types;

pub use error::{NapiError, NapiResult};
pub use types::{JsQueryRequest, JsSearchHit, JsSearchScope, JsTagFilter, QueryRequest};

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, RwLock, RwLockReadGuard, RwLockWriteGuard};

use pack_core::{PackReader, PackStore, SearchEngine, SearchHit, SearchQuery};
use tempfile::TempDir;

/// Wire-stable handle to an open pack. JS sees this as ``BigInt``
/// (preserving the full 64-bit width without precision loss; JS
/// ``Number`` only has 53 mantissa bits).
///
/// ``0`` is the reserved "no handle" sentinel: the registry's
/// allocator starts at ``1`` and never re-mints ``0``, so any call
/// from JS that passes ``0`` is guaranteed to be rejected with
/// [`NapiError::InvalidArgument`].
pub type PackHandle = u64;

/// Sentinel "no handle" value mirroring the JS ``0n`` placeholder
/// hosts use before calling [`open_pack`].
pub const NONE_HANDLE: PackHandle = 0;

/// One open pack in the registry. The tempdir is kept alive
/// alongside the store so dropping the registry entry on
/// [`close_pack`] also cleans up the extracted SQLite file on
/// disk.
///
/// **Field order matters** — Rust drops struct fields in
/// declaration order, and we need ``store`` (which owns a
/// [`rusqlite::Connection`] holding an open file handle on the
/// SQLite file) to drop *before* ``_tempdir`` (which removes the
/// directory containing that file). On Windows, ``remove_dir_all``
/// would otherwise fail because the file is still locked by the
/// open connection, and ``TempDir::drop`` swallows the error,
/// leaking the temp directory silently.
struct OpenPack {
    store: Mutex<PackStore>,
    /// Hold the [`TempDir`] for the lifetime of the entry — when
    /// the registry drops the [`Arc<OpenPack>`], the tempdir is
    /// removed along with the extracted SQLite file. Marked with
    /// an underscore-prefixed name because we never read the field
    /// after construction; the RAII drop is the whole point.
    _tempdir: TempDir,
}

// ``PackStore`` wraps a ``rusqlite::Connection`` which is ``Send``
// but ``!Sync``. ``Mutex<PackStore>`` adds ``Sync`` via the lock,
// so ``OpenPack`` is fully ``Send + Sync`` — verified at compile
// time by the assertion below. Wrapped in ``const _ : ()`` so the
// check has zero runtime cost.
const _: () = {
    fn _assert_send_sync<T: Send + Sync>() {}
    fn _check() {
        _assert_send_sync::<OpenPack>();
        _assert_send_sync::<Arc<OpenPack>>();
    }
};

type Registry = RwLock<HashMap<PackHandle, Arc<OpenPack>>>;

fn registry() -> &'static Registry {
    static REGISTRY: OnceLock<Registry> = OnceLock::new();
    REGISTRY.get_or_init(|| RwLock::new(HashMap::new()))
}

fn read_registry() -> RwLockReadGuard<'static, HashMap<PackHandle, Arc<OpenPack>>> {
    // Poisoning here means a prior holder of the write lock
    // panicked while mutating the registry. We surface the inner
    // map regardless — losing the registry across the rest of the
    // process is strictly worse than continuing on a possibly
    // racy snapshot, and the only mutation we make on the write
    // side is an ``insert`` / ``remove`` which never leaves the
    // map in a half-baked state.
    registry()
        .read()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn write_registry() -> RwLockWriteGuard<'static, HashMap<PackHandle, Arc<OpenPack>>> {
    registry()
        .write()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Allocate the next handle. Monotonic — never re-mints a value,
/// so a use-after-close in JS is guaranteed to miss the registry
/// rather than alias a different pack.
fn next_handle() -> PackHandle {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    // ``Relaxed`` is sufficient: ``fetch_add`` is an atomic RMW so
    // each caller receives a distinct value regardless of
    // ordering between threads.
    NEXT.fetch_add(1, Ordering::Relaxed)
}

/// Open and verify a ``.pack`` file at ``path``, returning a
/// handle the host must pass back into [`search`] and
/// [`close_pack`].
///
/// The pack format ships an *unencrypted* SQLite blob (the
/// ``VACUUM INTO`` inside [`pack_core::PackBuilder`] writes a
/// plaintext copy of the in-memory store), so this opens the
/// extracted DB with an empty passphrase. Future encrypted-pack
/// support would extend the surface with an explicit passphrase
/// argument.
///
/// # Errors
///
/// * [`NapiError::Pack`] forwarding any [`pack_core::PackError`]
///   from header / checksum / schema validation, the temp extract
///   path, or the SQLite open.
/// * [`NapiError::Pack`] with ``kind = "Io"`` if the tempdir
///   could not be created.
pub fn open_pack(path: &str) -> NapiResult<PackHandle> {
    let reader = PackReader::open(Path::new(path))?;

    // ``tempfile::Builder`` produces a unique directory in the OS
    // temp dir. We hand-pick the prefix so the extracted DBs are
    // easy to spot in a ``ls`` of ``$TMPDIR`` while a session is
    // open.
    let dir = tempfile::Builder::new().prefix("pack-napi-").tempdir()?;
    let db_path = dir.path().join("pack.sqlite");
    reader.extract_to(&db_path)?;

    // Empty passphrase — the embedded SQLite blob is plaintext,
    // not SQLCipher-encrypted. See the doc comment above.
    let store = PackStore::open(&db_path, "")?;

    let handle = next_handle();
    let entry = Arc::new(OpenPack {
        store: Mutex::new(store),
        _tempdir: dir,
    });
    write_registry().insert(handle, entry);
    Ok(handle)
}

/// Run a query against the pack identified by ``handle``.
///
/// Returns at most ``request.limit`` hits (default 10) in
/// descending [`SearchHit::rank_score`] order. See
/// [`pack_core::SearchEngine::search`] for the full ranking
/// formula.
///
/// # Errors
///
/// * [`NapiError::InvalidArgument`] if ``handle`` is the
///   [`NONE_HANDLE`] sentinel or is not present in the registry
///   (use-after-close, never-opened, or aliased from a different
///   process).
/// * [`NapiError::Pack`] forwarding any [`pack_core::PackError`]
///   from the underlying search engine (SQL prepare/exec, JSON
///   tag-decode, embedding-shape mismatch, ...).
pub fn search(handle: PackHandle, request: JsQueryRequest) -> NapiResult<Vec<SearchHit>> {
    if handle == NONE_HANDLE {
        return Err(NapiError::InvalidArgument {
            message: "handle is the reserved 0 sentinel, never returned by open_pack".into(),
        });
    }
    let entry =
        read_registry()
            .get(&handle)
            .cloned()
            .ok_or_else(|| NapiError::InvalidArgument {
                message: format!("unknown handle: {handle}"),
            })?;

    let query = SearchQuery {
        text: request.text,
        tags: request.tags.into(),
        query_embedding: request.query_embedding,
        semantic_model_tag: request.semantic_model_tag,
        limit: request.limit,
        scope: request.scope.into(),
        weights: None,
    };

    // Lock the per-pack mutex. Poisoning here means a previous
    // ``search`` panicked mid-query (e.g. an OOM inside rusqlite).
    // The store may be in a recoverable shape — the connection is
    // an autocommit handle so a panic between statements doesn't
    // leave a transaction open — so we recover from the poison
    // and re-run rather than failing the entire pack handle.
    let store = entry
        .store
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let engine = SearchEngine::new(&store);
    let hits = engine.search(&query)?;
    Ok(hits)
}

/// Drop the open store identified by ``handle``. Hosts should call
/// this in ``try`` / ``finally`` shutdown paths.
///
/// Idempotent: calling with an unknown handle (already closed,
/// never opened, or the [`NONE_HANDLE`] sentinel) is a no-op.
/// Returns ``true`` if a registry entry was removed, ``false``
/// otherwise. The boolean is the surface used by [`bindings`] to
/// signal "did a close actually happen" to JS callers that want
/// to telemetry on double-close patterns.
pub fn close_pack(handle: PackHandle) -> bool {
    if handle == NONE_HANDLE {
        return false;
    }
    // Removing under the write lock prevents a concurrent
    // ``search`` from grabbing a fresh ``Arc`` clone after we
    // commit to closing. An in-flight ``search`` already holding
    // an ``Arc`` runs to completion against that clone; the
    // ``OpenPack`` (and therefore the ``TempDir``) is dropped on
    // the last clone.
    write_registry().remove(&handle).is_some()
}

#[cfg(test)]
mod tests {
    //! Unit tests for the pure-Rust facade.
    //!
    //! Tests construct a real ``.pack`` file on disk using
    //! [`pack_core::PackBuilder`] over an in-memory store so they
    //! exercise the same open/extract/search path the production
    //! Node bridge takes. No network, no fixtures on disk.

    use super::*;
    use pack_core::{Chunk, ChunkingPolicy, Episode, PackBuilder, PackStore};

    /// Build a real ``.pack`` file on disk over a single
    /// publisher / series / episode / chunk. The fixture exercises
    /// the same ingest + build path the production pipeline runs.
    fn build_test_pack() -> (TempDir, std::path::PathBuf) {
        let dir = tempfile::Builder::new()
            .prefix("pack-napi-test-")
            .tempdir()
            .expect("tempdir");

        let store = PackStore::open_in_memory().expect("open in-memory store");

        let episode = Episode {
            episode_id: "test_publisher_flagship_widgets".into(),
            publisher: "test_publisher".into(),
            series: "flagship".into(),
            title: "Widgets".into(),
            host: vec!["Host One".into()],
            guest: vec![],
            publication_date: "2025-01-01".into(),
            country_region: vec!["US".into()],
            industry_tags: vec!["manufacturing".into()],
            function_tags: vec!["operations".into()],
            business_model_tags: vec!["B2B".into()],
            source_type: "podcast_transcript_html".into(),
            language: "en".into(),
            primary_url: "https://example.com/ep/1".into(),
            asset_urls: vec![],
            // ``free_access_copyrighted`` is in the default rights
            // allowlist, so the gate admits the episode and the
            // governance log records a normal ingest row.
            rights_code: "free_access_copyrighted".into(),
            rights_summary: "Test fixture only.".into(),
            credibility_notes: String::new(),
            summary: "A test episode about widget manufacturing.".into(),
            chunking_policy: Some(ChunkingPolicy {
                target_tokens: 700,
                overlap_tokens: 120,
            }),
        };
        store.ingest_episode(&episode).expect("ingest episode");

        let chunk = Chunk {
            chunk_id: "test_publisher_flagship_widgets#0001".into(),
            episode_id: episode.episode_id.clone(),
            token_count: 16,
            section_heading: Some("Manufacturing".into()),
            chunk_text: "HOST: Widgets are manufactured in batches of one hundred. \
                         The factory runs three shifts."
                .into(),
            citation_anchor: format!("{}#section-1", episode.primary_url),
        };
        store.ingest_chunk(&chunk).expect("ingest chunk");

        let pack_path = dir.path().join("test.pack");
        PackBuilder::new(&store)
            .with_build_notes("napi-test")
            .build_to(&pack_path)
            .expect("build pack");
        (dir, pack_path)
    }

    #[test]
    fn open_then_search_then_close_round_trip() {
        let (_dir, pack_path) = build_test_pack();
        let handle = open_pack(pack_path.to_str().unwrap()).expect("open_pack");
        assert_ne!(handle, NONE_HANDLE);

        let hits = search(
            handle,
            QueryRequest {
                text: "widgets".into(),
                limit: 5,
                ..Default::default()
            },
        )
        .expect("search");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].chunk_id, "test_publisher_flagship_widgets#0001");
        // BM25 surfaces a finite negative-or-positive score; pin
        // only that it's present.
        assert!(hits[0].bm25_score.is_some());

        let removed = close_pack(handle);
        assert!(removed);

        // After close, a follow-up search must fail.
        let err = search(
            handle,
            QueryRequest {
                text: "widgets".into(),
                ..Default::default()
            },
        )
        .expect_err("post-close search should fail");
        assert_eq!(err.kind(), "InvalidArgument");
    }

    #[test]
    fn search_with_zero_handle_is_rejected() {
        let err = search(NONE_HANDLE, QueryRequest::default()).expect_err("must reject");
        assert_eq!(err.kind(), "InvalidArgument");
    }

    #[test]
    fn close_unknown_handle_is_noop() {
        // ``999_999`` was never returned by ``next_handle`` in this
        // test process (the counter starts at 1 and only the
        // ``round_trip`` test increments it). A double-close on
        // an already-closed handle should also be a no-op.
        assert!(!close_pack(999_999));
        assert!(!close_pack(NONE_HANDLE));
    }

    #[test]
    fn multiple_packs_have_distinct_handles() {
        let (_dir1, pack1) = build_test_pack();
        let (_dir2, pack2) = build_test_pack();
        let h1 = open_pack(pack1.to_str().unwrap()).unwrap();
        let h2 = open_pack(pack2.to_str().unwrap()).unwrap();
        assert_ne!(h1, h2);
        assert!(close_pack(h1));
        assert!(close_pack(h2));
    }

    #[test]
    fn open_pack_on_bad_path_returns_io_kind() {
        let err = open_pack("/nonexistent/path/to/pack.bin").expect_err("missing pack");
        // The underlying ``File::open`` returns a ``NotFound`` io
        // error which ``pack_core`` wraps in ``PackError::Io``.
        assert_eq!(err.kind(), "Io");
    }

    #[test]
    fn open_pack_on_truncated_file_returns_truncated_pack_kind() {
        let dir = tempfile::Builder::new().tempdir().unwrap();
        let path = dir.path().join("truncated.pack");
        // 4 bytes — less than ``MIN_HEADER_BYTES`` (22). ``PackReader``
        // returns ``TruncatedPack``.
        std::fs::write(&path, b"SNMD").unwrap();
        let err = open_pack(path.to_str().unwrap()).expect_err("truncated pack");
        assert_eq!(err.kind(), "TruncatedPack");
    }
}
