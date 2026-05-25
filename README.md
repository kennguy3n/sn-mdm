# sn-mdm

A permanent, highly compact **content-pack builder** for on-device agents.

`sn-mdm` crawls freely accessible podcast transcripts and official
resource hubs, normalises the bodies, splits them into retrieval
chunks, runs them through a rights gate, and bundles the result
into a single compressed `.pack` file the agent can mount locally
for SQLCipher + FTS5 + optional semantic search.

The architecture borrows directly from two upstream substrates:

- [`kennguy3n/knowledge`](https://github.com/kennguy3n/knowledge) —
  evidence store (append-only, BLAKE3 content-hash dedup, FTS5
  virtual index, embeddings cache), connector framework
  (`initial_sync` / `incremental_sync` lifecycle), and the
  5-family metadata taxonomy.
- [`kennguy3n/chat-storage-search`](https://github.com/kennguy3n/chat-storage-search) —
  multi-lane query engine (BM25 + tag-filter + semantic merge),
  rank weights, and SQLCipher pragmas.

## Repository layout

```text
sn-mdm/
├── Cargo.toml                    # Rust workspace
├── crates/pack_core/             # SQLCipher + FTS5 store, search, .pack export
├── crawl/                        # Python crawl layer (17 source crawlers)
│   ├── crawl_config.toml         # Source registry — all 17 publishers
│   ├── crawlers/                 # Concrete crawlers + BaseCrawler
│   ├── pipeline.py               # Orchestrator (rights gate, chunker, JSONL)
│   └── tests/                    # Pure-Python unit tests
├── packs/                        # Output root
│   ├── raw/         normalized/  # Layers 1 & 2 (per-source HTML/PDF + markdown)
│   ├── metadata/    chunks/      # Layers 3 & 4 (JSONL streams)
│   └── governance/               # Layer 6 (rights audit trail)
└── docs/                         # ARCHITECTURE.md, SOURCES.md
```

## Pipeline

```text
crawl  ->  rights_gate  ->  normalize  ->  chunk  ->  tag  ->  metadata_emit  ->  governance_log
                                                                        |
                                                                        v
                                                              pack_core::ingest  ->  .pack
```

Every stage enforces a contract:

1. **Rights gate before chunking.** Episodes are rejected (with a
   permanent audit-log entry) unless their `rights_code` is in the
   allowlist (`ogl_v3`, `cc_by`, `cc_by_sa`, `cc_by_nc`,
   `cc_by_nc_nd`, `free_access_copyrighted`, `public_domain`).
2. **Speaker-turn + section-heading chunking** at 700-token target
   with 120-token overlap.
3. **Companion-resource asset URLs** are detected during
   normalisation and persisted onto the episode JSONL so the
   on-device agent can deep-link to whitepapers / playbooks
   referenced by the transcript.
4. **BLAKE3 content-hash dedup** at the chunk + episode level. Re-
   crawls are idempotent.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full
substrate cross-walk and [`docs/SOURCES.md`](docs/SOURCES.md) for
the curated source catalogue.

## Quick start

### Build the Rust crate

```bash
cargo test -p pack_core
```

### Run the Python crawl + chunk pipeline

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r crawl/requirements.txt

# Optional: populate `episodes = [...]` in crawl/crawl_config.toml
# for sources where you want an explicit seed set; otherwise each
# crawler walks the source's index page via initial_sync.

python -m crawl.pipeline acquired a16z bcg
```

Outputs land under `packs/`:

- `packs/raw/{publisher}/{slug}.{html,pdf}`
- `packs/normalized/{publisher}/{slug}.md`
- `packs/metadata/{publisher}.jsonl`
- `packs/chunks/{publisher}.jsonl`
- `packs/governance/rights_log.jsonl`

### Build the on-device `.pack`

```rust
use pack_core::{PackBuilder, PackStore};
use std::path::Path;

let store = PackStore::open(Path::new("dist.sqlite"), "")?;
store.ingest_jsonl_files(Path::new("packs/"))?;
PackBuilder::new(&store)
    .with_build_notes(format!("{} @ {}", env!("CARGO_PKG_VERSION"), chrono::Utc::now()))
    .build_to("snmdm-2025-q4.pack")?;
```

The resulting `.pack` is a zstd-compressed SQLite database
framed with a manifest the agent verifies before mount.

## Testing

```bash
cargo test --all                          # Rust unit tests
pytest crawl/tests                        # Python unit tests (no network)
cargo fmt --all -- --check && cargo clippy --all-targets -- -D warnings
ruff check crawl
```

## License

Dual-licensed under Apache-2.0 OR MIT.
